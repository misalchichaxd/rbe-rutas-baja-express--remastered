"""
elipse_bridge.py - Conexion de RBE (Django) con ELIPSE ENGINE.

Flujo:
  Navegador --POST /elipse/ask/-----------> Django --POST /v1/chat--> Engine   (inicia la tarea)
  Navegador --GET  /elipse/ask/<task_id>/-> Django --GET  /v1/task--> Engine   (sondeo)
  Engine (cuando el modelo pide una tool) --POST /elipse/callback/<tool> firmado HMAC--> Django
       Django valida firma, ejecuta la consulta de solo lectura (elipse_tools) y responde {"ok":..,"result":..}

Variables de entorno (.env):
  ELIPSE_ENGINE_URL          http://127.0.0.1:8001            (URL base del Engine, sin / al final)
  ELIPSE_API_KEY             elp_...                          (llave del perfil 'rbe'; solo vive en el servidor)
  ELIPSE_CALLBACK_SECRET     <secreto de set-callback>        (el mismo que genero el Engine)
  ELIPSE_PROFILE             rbe                              (opcional)
  ELIPSE_SOLO_SUPERVISORES   True                             (opcional; False = cualquier taquillero)
  ELIPSE_MAX_SKEW            90                               (opcional; segundos de tolerancia de reloj)
  ELIPSE_RATE_PER_MIN        15                               (opcional; preguntas por minuto por usuario)
"""

import hashlib
import hmac
import html
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request

from django.core.cache import cache
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from . import elipse_tools
from .models import Taquillero

log = logging.getLogger("elipse")

TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
MAX_PREGUNTA = 2000
MAX_CALLBACK_BODY = 64 * 1024


def _cfg():
    return {
        "url": os.getenv("ELIPSE_ENGINE_URL", "").strip().rstrip("/"),
        "key": os.getenv("ELIPSE_API_KEY", "").strip(),
        "secret": os.getenv("ELIPSE_CALLBACK_SECRET", "").strip(),
        "perfil": os.getenv("ELIPSE_PROFILE", "rbe").strip() or "rbe",
        "solo_supervisores": os.getenv("ELIPSE_SOLO_SUPERVISORES", "True") == "True",
        "skew": int(os.getenv("ELIPSE_MAX_SKEW", "90") or 90),
        "rate": int(os.getenv("ELIPSE_RATE_PER_MIN", "15") or 15),
        "timeout": float(os.getenv("ELIPSE_HTTP_TIMEOUT", "15") or 15),
    }


# --------------------------------------------------------------------------- firma HMAC (lado anfitrion)
# Misma formula que app/core/callback.py del Engine:
#   HMAC_SHA256(secret, f"{ts}.{perfil}.{tool}.{sha256_hex(body)}")

def firmar(secret, ts, perfil, tool, body: bytes) -> str:
    msg = "%s.%s.%s.%s" % (ts, perfil, tool, hashlib.sha256(body).hexdigest())
    return hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()


def verificar(secret, perfil, tool, body: bytes, ts, sig, max_skew=90, ahora=None) -> bool:
    try:
        ts_i = int(ts)
    except (TypeError, ValueError):
        return False
    if abs((ahora if ahora is not None else time.time()) - ts_i) > max_skew:
        return False
    return hmac.compare_digest(firmar(secret, ts_i, perfil, tool, body), str(sig or ""))


# --------------------------------------------------------------------------- cliente del Engine

class EngineError(Exception):
    def __init__(self, mensaje, status=502):
        super().__init__(mensaje)
        self.mensaje, self.status = mensaje, status


def _engine(method, path, payload=None):
    cfg = _cfg()
    if not cfg["url"] or not cfg["key"]:
        raise EngineError("Elipse no esta configurado en el servidor.", 503)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        cfg["url"] + path, data=data, method=method,
        headers={"Authorization": "Bearer " + cfg["key"], "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=cfg["timeout"]) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detalle = e.read().decode("utf-8", "ignore")[:300]
        log.warning("Engine respondio HTTP %s en %s: %s", e.code, path, detalle)
        if e.code in (401, 403):
            raise EngineError("Elipse rechazo la llave del servidor. Avisa al administrador.", 502)
        if e.code == 409:
            raise EngineError("El perfil de Elipse aun no esta listo (falta sincronizar su configuracion).", 503)
        raise EngineError("Elipse no pudo procesar la solicitud.", 502)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        log.warning("No se pudo contactar al Engine (%s): %s", path, e)
        raise EngineError("No se pudo conectar con Elipse. Intenta de nuevo en un momento.", 502)


# --------------------------------------------------------------------------- respuesta -> HTML seguro

def _inline(s):
    s = html.escape(s, quote=False)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![\*\w])\*(?!\s)(.+?)(?<!\s)\*(?![\*\w])", r"<em>\1</em>", s)
    return s


def _celdas(linea):
    return [c.strip() for c in linea.strip().strip("|").split("|")]


def markdown_a_html(texto: str) -> str:
    """Subconjunto de Markdown (parrafos, listas, tablas, **negrita**) con TODO el texto escapado."""
    texto = re.sub(r"<think>.*?</think>", "", texto or "", flags=re.S | re.I).strip()
    if not texto:
        return "<p>(sin respuesta)</p>"
    lineas, out, i = texto.splitlines(), [], 0
    parrafo, lista = [], None

    def cerrar():
        nonlocal parrafo, lista
        if parrafo:
            out.append("<p>%s</p>" % "<br>".join(_inline(x) for x in parrafo))
            parrafo = []
        if lista:
            out.append("</%s>" % lista)
            lista = None

    while i < len(lineas):
        ln = lineas[i].rstrip()
        s = ln.strip()
        if not s:
            cerrar()
        elif s.startswith("|") and i + 1 < len(lineas) and re.match(r"^\s*\|?[\s:\-|]+\|?\s*$", lineas[i + 1]) \
                and "-" in lineas[i + 1]:
            cerrar()
            cab = _celdas(s)
            i += 2
            filas = []
            while i < len(lineas) and lineas[i].strip().startswith("|"):
                filas.append(_celdas(lineas[i]))
                i += 1
            out.append("<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>" % (
                "".join("<th>%s</th>" % _inline(c) for c in cab),
                "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % _inline(c) for c in f) for f in filas)))
            continue
        elif re.match(r"^#{1,4}\s+", s):
            cerrar()
            out.append("<p><strong>%s</strong></p>" % _inline(re.sub(r"^#{1,4}\s+", "", s)))
        elif re.match(r"^[-*\u2022]\s+", s) or re.match(r"^\d+[.)]\s+", s):
            tipo = "ul" if re.match(r"^[-*\u2022]\s+", s) else "ol"
            if parrafo:
                out.append("<p>%s</p>" % "<br>".join(_inline(x) for x in parrafo))
                parrafo = []
            if lista != tipo:
                if lista:
                    out.append("</%s>" % lista)
                out.append("<%s>" % tipo)
                lista = tipo
            out.append("<li>%s</li>" % _inline(re.sub(r"^([-*\u2022]|\d+[.)])\s+", "", s)))
        else:
            if lista:
                out.append("</%s>" % lista)
                lista = None
            parrafo.append(s)
        i += 1
    cerrar()
    return "".join(out)


# --------------------------------------------------------------------------- autorizacion

def _usuario(request):
    """usuario_id si la sesion puede usar Elipse; si no, (None, codigo_http)."""
    uid = request.session.get("usuario_id")
    if not uid:
        return None, 401
    if _cfg()["solo_supervisores"] and not request.session.get("supervisa"):
        return None, 403
    return uid, 200


def _hilo(request, uid, nuevo=False):
    """Id de conversacion que ve el Engine (su historial es por perfil+sesion). Cambia al limpiar o recargar."""
    if nuevo:
        request.session["elipse_hilo"] = int(request.session.get("elipse_hilo", 0)) + 1
    return "u%s-%d" % (uid, int(request.session.get("elipse_hilo", 0)))


def _limite_ok(uid):
    cfg = _cfg()
    clave = "elipse:rl:%s:%d" % (uid, int(time.time() // 60))
    cache.add(clave, 0, 90)
    try:
        return cache.incr(clave) <= cfg["rate"]
    except ValueError:
        return True


# --------------------------------------------------------------------------- vistas del chat

def elipse_page(request):
    uid, codigo = _usuario(request)
    if codigo == 401:
        from django.shortcuts import redirect
        return redirect("login")
    if codigo == 403:
        return JsonResponse({"error": "Elipse es solo para supervisores."}, status=403)
    _hilo(request, uid, nuevo=True)      # recargar la pagina = conversacion nueva (como antes)
    return render(request, "taquilla/elipse.html", {})


@require_POST
def elipse_nuevo(request):
    """Boton 'Limpiar chat': el Engine empieza una conversacion sin el historial anterior."""
    uid, codigo = _usuario(request)
    if codigo != 200:
        return JsonResponse({"error": "Sesion no valida."}, status=codigo)
    _hilo(request, uid, nuevo=True)
    return JsonResponse({"ok": True})


@require_POST
def elipse_ask(request):
    uid, codigo = _usuario(request)
    if codigo != 200:
        return JsonResponse({"error": "Sesion no valida o sin permiso para Elipse."}, status=codigo)
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        return JsonResponse({"error": "Solicitud invalida."}, status=400)
    pregunta = str(body.get("pregunta") or "").strip()[:MAX_PREGUNTA]
    if not pregunta:
        return JsonResponse({"error": "Escribe una pregunta."}, status=400)
    if not _limite_ok(uid):
        return JsonResponse({"error": "Demasiadas preguntas seguidas. Espera un momento."}, status=429)
    try:
        payload = {"message": pregunta, "session": _hilo(request, uid), "actor": str(uid)}
        if "voz" in body:                       # el boton de voz del chat; sin el, manda el modo del perfil
            payload["speak"] = bool(body.get("voz"))
        resp = _engine("POST", "/v1/chat", payload)
    except EngineError as e:
        return JsonResponse({"error": e.mensaje}, status=e.status)
    task_id = str(resp.get("task_id") or "")
    if not TASK_ID_RE.match(task_id):
        return JsonResponse({"error": "Respuesta inesperada de Elipse."}, status=502)
    # La llave del Engine es una sola para todo RBE: la tarea se liga a la SESION del usuario que la creo.
    tareas = request.session.get("elipse_tareas", [])
    request.session["elipse_tareas"] = (tareas + [task_id])[-20:]
    return JsonResponse({"task_id": task_id})


@require_GET
def elipse_poll(request, task_id):
    uid, codigo = _usuario(request)
    if codigo != 200:
        return JsonResponse({"error": "Sesion no valida."}, status=codigo)
    if not TASK_ID_RE.match(task_id) or task_id not in request.session.get("elipse_tareas", []):
        return JsonResponse({"estado": "error", "error": "Esa consulta no existe."}, status=404)
    try:
        data = _engine("GET", "/v1/task/" + task_id)
    except EngineError as e:
        return JsonResponse({"estado": "error", "error": e.mensaje}, status=e.status)
    estado = data.get("status")
    resultado = data.get("result") if isinstance(data.get("result"), dict) else {}
    if estado == "en_progreso":
        return JsonResponse({"estado": "en_progreso"})
    if estado == "completado":
        return JsonResponse({"estado": "listo", "voz": bool(resultado.get("voice")),
                             "html": markdown_a_html(str(resultado.get("reply") or ""))})
    if estado == "cancelado":
        return JsonResponse({"estado": "error", "error": "La consulta fue cancelada."})
    log.warning("Tarea %s termino con estado %s: %s", task_id, estado, str(resultado)[:300])
    return JsonResponse({"estado": "error", "error": "Elipse no pudo completar la consulta."})


@require_GET
def elipse_voz(request, task_id):
    """Audio de la respuesta de una consulta propia. Django hace de intermediario: la llave del Engine no sale del servidor."""
    uid, codigo = _usuario(request)
    if codigo != 200:
        return JsonResponse({"error": "Sesion no valida."}, status=codigo)
    if not TASK_ID_RE.match(task_id) or task_id not in request.session.get("elipse_tareas", []):
        return JsonResponse({"error": "Esa consulta no existe."}, status=404)
    cfg = _cfg()
    if not cfg["url"] or not cfg["key"]:
        return JsonResponse({"error": "Elipse no esta configurado en el servidor."}, status=503)
    req = urllib.request.Request(cfg["url"] + "/v1/voice/" + task_id + "/stream",
                                 headers={"Authorization": "Bearer " + cfg["key"]})
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            if res.status == 204:
                return HttpResponse(status=204)
            audio, tipo = res.read(), res.headers.get("Content-Type", "audio/mpeg")
    except urllib.error.HTTPError as e:
        log.warning("Voz: el Engine respondio HTTP %s para la tarea %s", e.code, task_id)
        return JsonResponse({"error": "No se pudo generar la voz."}, status=502)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning("Voz: no se pudo contactar al Engine: %s", e)
        return JsonResponse({"error": "No se pudo conectar con Elipse."}, status=502)
    resp = HttpResponse(audio, content_type=tipo)
    resp["Cache-Control"] = "no-store"
    return resp


@require_GET
def elipse_estado(request):
    """Diagnostico para supervisores: ¿esta configurado y alcanzable el Engine?"""
    uid, codigo = _usuario(request)
    if codigo != 200:
        return JsonResponse({"error": "Sin permiso."}, status=codigo)
    cfg = _cfg()
    out = {"configurado": bool(cfg["url"] and cfg["key"]), "callback_secret": bool(cfg["secret"]),
           "perfil": cfg["perfil"], "motor": None}
    if out["configurado"]:
        try:
            req = urllib.request.Request(cfg["url"] + "/v1/status")
            with urllib.request.urlopen(req, timeout=cfg["timeout"]) as res:
                out["motor"] = json.loads(res.read().decode("utf-8"))
        except Exception as e:
            out["motor"] = "no responde: %s" % type(e).__name__
    return JsonResponse(out)


# --------------------------------------------------------------------------- callback del Engine

def _resp(ok, **kw):
    return JsonResponse(dict(ok=ok, **kw), json_dumps_params={"ensure_ascii": False})


def _actor_autorizado(actor: str) -> bool:
    if not actor.isdigit():
        return False
    qs = Taquillero.objects.filter(registro=int(actor))
    if _cfg()["solo_supervisores"]:
        qs = qs.filter(supervisa=1)
    return qs.exists()


@csrf_exempt
@require_POST
def elipse_callback(request, tool):
    cfg = _cfg()
    if not cfg["secret"]:
        return _resp(False, error="callback no configurado")
    body = request.body
    if len(body) > MAX_CALLBACK_BODY:
        return _resp(False, error="cuerpo demasiado grande")
    if not verificar(cfg["secret"], cfg["perfil"], tool, body,
                     request.headers.get("X-Elipse-Ts"), request.headers.get("X-Elipse-Sig"), cfg["skew"]):
        log.warning("Callback con firma invalida (tool=%s, ip=%s)", tool, request.META.get("REMOTE_ADDR"))
        return JsonResponse({"ok": False, "error": "firma invalida"}, status=401)
    try:
        payload = json.loads(body)
    except ValueError:
        return JsonResponse({"ok": False, "error": "JSON invalido"}, status=400)
    if not isinstance(payload, dict) or payload.get("tool") != tool:
        return JsonResponse({"ok": False, "error": "tool no coincide con la URL"}, status=400)
    call_id = str(payload.get("call_id") or "")[:64]
    if not call_id or not cache.add("elipse:cb:" + call_id, 1, cfg["skew"] * 2):
        return JsonResponse({"ok": False, "error": "llamada repetida"}, status=409)   # anti-replay
    if not _actor_autorizado(str(payload.get("actor") or "")):
        return JsonResponse({"ok": False, "error": "usuario sin permiso para esta consulta"}, status=403)
    fn = elipse_tools.TOOLS.get(tool)
    if fn is None:
        return JsonResponse({"ok": False, "error": "herramienta desconocida"}, status=404)
    args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
    try:
        return _resp(True, result=fn(args))
    except elipse_tools.ToolError as e:
        return _resp(False, error=str(e))
    except Exception:
        log.exception("Fallo la tool %s", tool)
        return _resp(False, error="error interno al consultar la base de datos")