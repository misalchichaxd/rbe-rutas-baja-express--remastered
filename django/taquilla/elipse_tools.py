"""
elipse_tools.py - Herramientas de RBE que ELIPSE ENGINE puede llamar (solo lectura).

Reemplaza los ~54 intents y el SQL generado por IA de elipse_views.py. Cada tool:
  * recibe argumentos ya validados por este módulo (nunca SQL),
  * ejecuta consultas FIJAS y parametrizadas,
  * devuelve datos compactos (dict) que el modelo redacta en lenguaje natural.

Nunca se expone: taquillero.contrasena, taquillero.usuario, cuenta_pasajero.clave, correos,
telefonos ni fechas de nacimiento de pasajeros.

TOOLS    -> {nombre: funcion(args) -> dict}
SCHEMA   -> definicion de las tools para el spec del perfil 'rbe' (tools.host)
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.conf import settings
from django.db import connection

MAX_FILAS = 40          # tope de filas que se devuelven al modelo (el Engine ademas trunca la salida)
MAX_RANGO_DIAS = 400    # evita consultas de rango enorme


class ToolError(Exception):
    """Mensaje corto y seguro para el usuario/modelo: se devuelve como {"ok": false, "error": ...}."""


# --------------------------------------------------------------------------- utilidades

def _hoy() -> date:
    """Fecha de hoy en la zona horaria del proyecto (Railway corre en UTC)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(settings.TIME_ZONE)).date()
    except Exception:
        return date.today()


def _val(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime, date, time)):
        return v.isoformat(sep=" ") if isinstance(v, datetime) else v.isoformat()
    if isinstance(v, timedelta):
        total = int(v.total_seconds())
        return "%02d:%02d" % (total // 3600, (total % 3600) // 60)
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return v


def _q(sql, params=None):
    with connection.cursor() as cur:
        cur.execute(sql, params or [])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, (_val(v) for v in row))) for row in cur.fetchall()]


def _int(args, key, default=None, lo=1, hi=10**9):
    v = args.get(key, default)
    if v is None:
        return None
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise ToolError("'%s' debe ser un numero entero." % key)
    if not lo <= v <= hi:
        raise ToolError("'%s' debe estar entre %d y %d." % (key, lo, hi))
    return v


def _fecha(args, key):
    v = args.get(key)
    if v in (None, ""):
        return None
    try:
        return datetime.strptime(str(v).strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        raise ToolError("'%s' debe tener formato AAAA-MM-DD." % key)


def _texto(args, key, maxlen=60):
    v = args.get(key)
    if v is None:
        return None
    v = " ".join(str(v).split())
    return v[:maxlen] or None


def _opcion(args, key, validas, default):
    v = args.get(key, default)
    v = str(v).strip().lower() if v is not None else default
    if v not in validas:
        raise ToolError("'%s' debe ser uno de: %s." % (key, ", ".join(validas)))
    return v


def _rango(desde, hasta, defecto_desde, defecto_hasta):
    """(desde, hasta) inclusivos -> (inicio, fin_exclusivo) como 'AAAA-MM-DD'."""
    d = desde or defecto_desde
    h = hasta or defecto_hasta
    if h < d:
        d, h = h, d
    if (h - d).days > MAX_RANGO_DIAS:
        raise ToolError("El rango maximo es de %d dias." % MAX_RANGO_DIAS)
    return d.isoformat(), (h + timedelta(days=1)).isoformat()


def _like_tokens(expr, texto):
    """Cada palabra del texto debe aparecer en `expr` (AND). Escapa % _ \\."""
    palabras = [p for p in (texto or "").lower().split() if p][:4]
    if not palabras or sum(len(p) for p in palabras) < 2:
        raise ToolError("Escribe al menos 2 letras del nombre.")
    clauses, params = [], []
    for p in palabras:
        p = p.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append("LOWER(%s) LIKE %%s" % expr)
        params.append("%" + p + "%")
    return "(" + " AND ".join(clauses) + ")", params


def _recortar(filas, limite=MAX_FILAS):
    return filas[:limite], max(0, len(filas) - limite)


NOMBRE_PAS = "CONCAT(pa.paNombre,' ',pa.paPrimerApell,COALESCE(CONCAT(' ',pa.paSegundoApell),''))"
NOMBRE_CON = "CONCAT(c.conNombre,' ',c.conPrimerApell,COALESCE(CONCAT(' ',c.conSegundoApell),''))"

ESTADOS = {"disponible": 1, "en_ruta": 2, "finalizado": 3, "cancelado": 4, "retrasado": 5}

_VIAJE_FROM = (
    " FROM viaje v"
    " JOIN ruta r ON v.ruta = r.codigo"
    " JOIN terminal tor ON r.origen = tor.numero"
    " JOIN terminal tde ON r.destino = tde.numero"
    " JOIN ciudad co ON tor.ciudad = co.clave"
    " JOIN ciudad cd ON tde.ciudad = cd.clave"
    " JOIN edo_viaje ev ON v.estado = ev.numero"
    " LEFT JOIN autobus a ON v.autobus = a.numero"
    " LEFT JOIN conductor c ON v.conductor = c.registro"
)


# --------------------------------------------------------------------------- tools

def resumen_dia(args):
    dia = _fecha(args, "fecha") or _hoy()
    ini, fin = dia.isoformat(), (dia + timedelta(days=1)).isoformat()
    viajes = _q(
        "SELECT ev.nombre AS estado, COUNT(*) AS viajes FROM viaje v"
        " JOIN edo_viaje ev ON v.estado = ev.numero"
        " WHERE v.fecHoraSalida >= %s AND v.fecHoraSalida < %s GROUP BY ev.nombre", [ini, fin])
    en_ruta = _q("SELECT COUNT(*) AS n FROM viaje WHERE estado = 2")[0]["n"]
    boletos = _q(
        "SELECT COUNT(*) AS boletos, COALESCE(SUM(precio),0) AS importe FROM ticket"
        " WHERE fechaEmision >= %s AND fechaEmision < %s", [ini, fin])[0]
    caja = _q(
        "SELECT tp.nombre AS metodo, COUNT(*) AS pagos, COALESCE(SUM(p.monto),0) AS total"
        " FROM pago p JOIN tipo_pago tp ON tp.numero = p.tipo"
        " WHERE p.fechapago >= %s AND p.fechapago < %s GROUP BY tp.nombre", [ini, fin])
    return {
        "fecha": ini,
        "viajes_del_dia_por_estado": viajes,
        "viajes_en_ruta_ahora": en_ruta,
        "boletos_emitidos": boletos["boletos"],
        "importe_boletos": boletos["importe"],
        "caja_por_metodo": caja,
        "ingresos_caja_total": round(sum(r["total"] for r in caja), 2),
    }


def buscar_viajes(args):
    desde, hasta = _fecha(args, "desde"), _fecha(args, "hasta")
    hoy = _hoy()
    ini, fin = _rango(desde, hasta, hoy, hoy)
    where = ["v.fecHoraSalida >= %s", "v.fecHoraSalida < %s"]
    params = [ini, fin]

    estado = _texto(args, "estado", 20)
    if estado:
        estado = estado.lower().replace(" ", "_")
        if estado not in ESTADOS:
            raise ToolError("'estado' debe ser uno de: %s." % ", ".join(ESTADOS))
        where.append("v.estado = %s")
        params.append(ESTADOS[estado])
    for clave, col in (("origen", "co.nombre"), ("destino", "cd.nombre")):
        t = _texto(args, clave, 30)
        if t:
            cl, pr = _like_tokens(col, t)
            where.append(cl)
            params += pr
    cond = _texto(args, "conductor", 60)
    if cond:
        cl, pr = _like_tokens(NOMBRE_CON, cond)
        where.append(cl)
        params += pr
    bus = _texto(args, "autobus", 12)
    if bus:
        if bus.isdigit():
            where.append("v.autobus = %s")
            params.append(int(bus))
        else:
            where.append("a.placas = %s")
            params.append(bus.upper())

    limite = _int(args, "limite", 20, 1, MAX_FILAS)
    cond_sql = " WHERE " + " AND ".join(where)
    total = _q("SELECT COUNT(*) AS n" + _VIAJE_FROM + cond_sql, params)[0]["n"]
    orden = "DESC" if (hasta or desde) and (hasta or hoy) < hoy else "ASC"
    filas = _q(
        "SELECT v.numero AS viaje, v.fecHoraSalida AS salida, v.fecHoraEntrada AS llegada_est,"
        " CONCAT(co.nombre,' a ',cd.nombre) AS ruta, ev.nombre AS estado,"
        " a.placas AS autobus, " + NOMBRE_CON + " AS conductor,"
        " (SELECT COUNT(*) FROM ticket t WHERE t.viaje = v.numero) AS pasajeros"
        + _VIAJE_FROM + cond_sql + " ORDER BY v.fecHoraSalida " + orden + " LIMIT %s",
        params + [limite])
    return {"desde": ini, "hasta_exclusivo": fin, "total": total,
            "mostrados": len(filas), "viajes": filas}


def detalle_viaje(args):
    num = _int(args, "viaje")
    if num is None:
        raise ToolError("Falta el numero de viaje.")
    filas = _q(
        "SELECT v.numero AS viaje, v.fecHoraSalida AS salida, v.fecHoraEntrada AS llegada_est,"
        " CONCAT(co.nombre,' a ',cd.nombre) AS ruta, tor.nombre AS terminal_origen,"
        " tde.nombre AS terminal_destino, r.precio AS precio_ruta, ev.nombre AS estado,"
        " a.numero AS autobus_num, a.placas AS autobus, mo.numasientos AS capacidad,"
        " " + NOMBRE_CON + " AS conductor, c.licVencimiento AS licencia_vence,"
        " (SELECT COUNT(*) FROM ticket t WHERE t.viaje = v.numero) AS pasajeros"
        + _VIAJE_FROM + " LEFT JOIN modelo mo ON a.modelo = mo.numero WHERE v.numero = %s", [num])
    if not filas:
        hay_folio = _q("SELECT numero FROM pago WHERE numero = %s", [num])
        if hay_folio:
            raise ToolError(
                "No existe el viaje #%d, pero si hay una compra con ese folio "
                "(el boarding pass muestra el folio). Usa consultar_boleto con folio=%d." % (num, num))
        raise ToolError("No existe el viaje #%d." % num)
    return filas[0]


def pasajeros_viaje(args):
    num = _int(args, "viaje")
    if num is None:
        raise ToolError("Falta el numero de viaje.")
    if not _q("SELECT numero FROM viaje WHERE numero = %s", [num]):
        raise ToolError("No existe el viaje #%d." % num)
    filas = _q(
        "SELECT tk.codigo AS ticket, " + NOMBRE_PAS + " AS pasajero,"
        " tpas.descripcion AS tipo_pasajero,"
        " COALESCE(tk.etiqueta_asiento, CAST(tk.asiento AS CHAR)) AS asiento, tk.precio AS precio"
        " FROM ticket tk JOIN pasajero pa ON pa.num = tk.pasajero"
        " LEFT JOIN tipo_pasajero tpas ON tpas.num = tk.tipopasajero"
        " WHERE tk.viaje = %s ORDER BY pa.paPrimerApell, pa.paNombre", [num])
    vis, extra = _recortar(filas)
    return {"viaje": num, "total_pasajeros": len(filas), "pasajeros": vis, "filas_omitidas": extra}


def consultar_boleto(args):
    folio, ticket = _int(args, "folio"), _int(args, "ticket")
    if folio is None and ticket is None:
        raise ToolError("Indica 'folio' (compra) o 'ticket' (boleto individual).")
    origen_ticket = None
    if folio is None:
        fila = _q("SELECT pago FROM ticket WHERE codigo = %s", [ticket])
        if not fila:
            raise ToolError("No existe el ticket #%d." % ticket)
        folio, origen_ticket = fila[0]["pago"], ticket
    compra = _q(
        "SELECT p.numero AS folio, p.fechapago AS fecha_compra, p.monto AS monto_total,"
        " tp.nombre AS metodo_pago,"
        " COALESCE(CONCAT(tq.taqNombre,' ',tq.taqPrimerApell),'App') AS vendedor"
        " FROM pago p LEFT JOIN tipo_pago tp ON tp.numero = p.tipo"
        " LEFT JOIN taquillero tq ON tq.registro = p.vendedor WHERE p.numero = %s", [folio])
    if not compra:
        raise ToolError("No existe una compra con folio #%d." % folio)
    boletos = _q(
        "SELECT tk.codigo AS ticket, tk.viaje AS viaje, " + NOMBRE_PAS + " AS pasajero,"
        " tpas.descripcion AS tipo_pasajero,"
        " COALESCE(tk.etiqueta_asiento, CAST(tk.asiento AS CHAR)) AS asiento,"
        " tk.precio AS precio, v.fecHoraSalida AS salida,"
        " CONCAT(co.nombre,' a ',cd.nombre) AS ruta, ev.nombre AS estado_viaje"
        " FROM ticket tk JOIN pasajero pa ON pa.num = tk.pasajero"
        " LEFT JOIN tipo_pasajero tpas ON tpas.num = tk.tipopasajero"
        " LEFT JOIN viaje v ON v.numero = tk.viaje"
        " LEFT JOIN ruta r ON r.codigo = v.ruta"
        " LEFT JOIN terminal tor ON tor.numero = r.origen"
        " LEFT JOIN terminal tde ON tde.numero = r.destino"
        " LEFT JOIN ciudad co ON co.clave = tor.ciudad"
        " LEFT JOIN ciudad cd ON cd.clave = tde.ciudad"
        " LEFT JOIN edo_viaje ev ON ev.numero = v.estado"
        " WHERE tk.pago = %s ORDER BY tk.codigo", [folio])
    out = {"compra": compra[0], "boletos": boletos}
    if origen_ticket:
        out["ticket_consultado"] = origen_ticket
    return out


def ingresos(args):
    hoy = _hoy()
    ini, fin = _rango(_fecha(args, "desde"), _fecha(args, "hasta"), hoy.replace(day=1), hoy)
    agrupar = _opcion(args, "agrupar_por", ("ninguno", "dia", "mes", "anio", "metodo"), "ninguno")
    base = " FROM pago p JOIN tipo_pago tp ON tp.numero = p.tipo" \
           " WHERE p.fechapago >= %s AND p.fechapago < %s"
    tot = _q("SELECT COUNT(*) AS pagos, COALESCE(SUM(p.monto),0) AS total" + base, [ini, fin])[0]
    out = {"desde": ini, "hasta_exclusivo": fin, "pagos": tot["pagos"], "total": tot["total"]}
    claves = {
        "dia": "DATE(p.fechapago)",
        "mes": "CONCAT(YEAR(p.fechapago),'-',LPAD(MONTH(p.fechapago),2,'0'))",
        "anio": "YEAR(p.fechapago)",
        "metodo": "tp.nombre",
    }
    if agrupar != "ninguno":
        expr = claves[agrupar]
        filas = _q("SELECT " + expr + " AS periodo, COUNT(*) AS pagos, SUM(p.monto) AS total"
                   + base + " GROUP BY " + expr + " ORDER BY " + expr, [ini, fin])
        vis, extra = _recortar(filas, 100)
        out.update({"agrupado_por": agrupar, "detalle": vis, "filas_omitidas": extra})
    return out


def boletos_vendidos(args):
    hoy = _hoy()
    ini, fin = _rango(_fecha(args, "desde"), _fecha(args, "hasta"), hoy, hoy)
    agrupar = _opcion(args, "agrupar_por", ("ninguno", "dia", "ruta"), "ninguno")
    tot = _q("SELECT COUNT(*) AS boletos, COALESCE(SUM(precio),0) AS importe FROM ticket"
             " WHERE fechaEmision >= %s AND fechaEmision < %s", [ini, fin])[0]
    out = {"desde": ini, "hasta_exclusivo": fin, "boletos": tot["boletos"], "importe": tot["importe"]}
    if agrupar == "dia":
        out["detalle"] = _q(
            "SELECT DATE(fechaEmision) AS dia, COUNT(*) AS boletos, SUM(precio) AS importe FROM ticket"
            " WHERE fechaEmision >= %s AND fechaEmision < %s GROUP BY DATE(fechaEmision)"
            " ORDER BY dia LIMIT 100", [ini, fin])
    elif agrupar == "ruta":
        out["detalle"] = _q(
            "SELECT CONCAT(co.nombre,' a ',cd.nombre) AS ruta, COUNT(*) AS boletos, SUM(tk.precio) AS importe"
            " FROM ticket tk JOIN viaje v ON v.numero = tk.viaje JOIN ruta r ON r.codigo = v.ruta"
            " JOIN terminal tor ON tor.numero = r.origen JOIN terminal tde ON tde.numero = r.destino"
            " JOIN ciudad co ON co.clave = tor.ciudad JOIN ciudad cd ON cd.clave = tde.ciudad"
            " WHERE tk.fechaEmision >= %s AND tk.fechaEmision < %s"
            " GROUP BY r.codigo, co.nombre, cd.nombre ORDER BY boletos DESC LIMIT 30", [ini, fin])
    return out


def ranking(args):
    entidad = _opcion(args, "entidad", ("conductores", "autobuses", "pasajeros", "rutas", "taquilleros"), None)
    n = _int(args, "n", 5, 1, 20)
    desde, hasta = _fecha(args, "desde"), _fecha(args, "hasta")
    hoy = _hoy()
    if desde or hasta:
        ini, fin = _rango(desde, hasta, hoy - timedelta(days=30), hoy)
    else:
        ini, fin = "1900-01-01", (hoy + timedelta(days=3660)).isoformat()   # sin filtro de fechas
    rutas_join = (" JOIN ruta r ON r.codigo = v.ruta JOIN terminal tor ON tor.numero = r.origen"
                  " JOIN terminal tde ON tde.numero = r.destino JOIN ciudad co ON co.clave = tor.ciudad"
                  " JOIN ciudad cd ON cd.clave = tde.ciudad")
    if entidad == "conductores":
        sql = ("SELECT " + NOMBRE_CON + " AS conductor, COUNT(DISTINCT v.numero) AS viajes,"
               " COUNT(tk.codigo) AS pasajeros FROM conductor c JOIN viaje v ON v.conductor = c.registro"
               " LEFT JOIN ticket tk ON tk.viaje = v.numero"
               " WHERE v.fecHoraSalida >= %s AND v.fecHoraSalida < %s"
               " GROUP BY c.registro, c.conNombre, c.conPrimerApell, c.conSegundoApell"
               " ORDER BY viajes DESC LIMIT %s")
    elif entidad == "autobuses":
        sql = ("SELECT a.numero AS autobus, a.placas AS placas, ma.nombre AS marca, mo.nombre AS modelo,"
               " COUNT(DISTINCT v.numero) AS viajes, COUNT(tk.codigo) AS pasajeros"
               " FROM autobus a JOIN modelo mo ON a.modelo = mo.numero JOIN marca ma ON mo.marca = ma.numero"
               " JOIN viaje v ON v.autobus = a.numero LEFT JOIN ticket tk ON tk.viaje = v.numero"
               " WHERE v.fecHoraSalida >= %s AND v.fecHoraSalida < %s"
               " GROUP BY a.numero, a.placas, ma.nombre, mo.nombre ORDER BY viajes DESC LIMIT %s")
    elif entidad == "pasajeros":
        sql = ("SELECT " + NOMBRE_PAS + " AS pasajero, COUNT(tk.codigo) AS boletos, SUM(tk.precio) AS gasto"
               " FROM pasajero pa JOIN ticket tk ON tk.pasajero = pa.num"
               " WHERE tk.fechaEmision >= %s AND tk.fechaEmision < %s"
               " GROUP BY pa.num, pa.paNombre, pa.paPrimerApell, pa.paSegundoApell"
               " ORDER BY boletos DESC LIMIT %s")
    elif entidad == "rutas":
        sql = ("SELECT CONCAT(co.nombre,' a ',cd.nombre) AS ruta, r.precio AS precio,"
               " COUNT(tk.codigo) AS boletos, COUNT(DISTINCT v.numero) AS viajes"
               " FROM ticket tk JOIN viaje v ON v.numero = tk.viaje" + rutas_join +
               " WHERE v.fecHoraSalida >= %s AND v.fecHoraSalida < %s"
               " GROUP BY r.codigo, co.nombre, cd.nombre, r.precio ORDER BY boletos DESC LIMIT %s")
    else:  # taquilleros: por quien registro la venta (pago.vendedor)
        sql = ("SELECT CONCAT(tq.taqNombre,' ',tq.taqPrimerApell) AS taquillero, te.nombre AS terminal,"
               " COUNT(DISTINCT p.numero) AS compras, COUNT(tk.codigo) AS boletos,"
               " COALESCE(SUM(tk.precio),0) AS importe"
               " FROM pago p JOIN taquillero tq ON tq.registro = p.vendedor"
               " JOIN terminal te ON te.numero = tq.terminal LEFT JOIN ticket tk ON tk.pago = p.numero"
               " WHERE p.fechapago >= %s AND p.fechapago < %s"
               " GROUP BY tq.registro, tq.taqNombre, tq.taqPrimerApell, te.nombre"
               " ORDER BY boletos DESC LIMIT %s")
    filas = _q(sql, [ini, fin, n])
    return {"entidad": entidad, "periodo": "todo el historial" if ini == "1900-01-01" else
            "%s a %s" % (ini, fin), "ranking": filas}


def buscar_pasajero(args):
    nombre = _texto(args, "nombre", 60)
    clausula, params = _like_tokens(NOMBRE_PAS, nombre)
    filas = _q(
        "SELECT pa.num AS pasajero_num, " + NOMBRE_PAS + " AS nombre, COUNT(tk.codigo) AS boletos,"
        " COALESCE(SUM(tk.precio),0) AS gasto_total, MAX(tk.fechaEmision) AS ultima_compra"
        " FROM pasajero pa LEFT JOIN ticket tk ON tk.pasajero = pa.num WHERE " + clausula +
        " GROUP BY pa.num, pa.paNombre, pa.paPrimerApell, pa.paSegundoApell"
        " ORDER BY ultima_compra DESC LIMIT 10", params)
    if not filas:
        return {"encontrados": 0, "nota": "No hay pasajeros con ese nombre."}
    out = {"encontrados": len(filas), "pasajeros": filas}
    if len(filas) == 1 and filas[0]["boletos"]:
        out["ultimos_viajes"] = _q(
            "SELECT v.numero AS viaje, v.fecHoraSalida AS salida,"
            " CONCAT(co.nombre,' a ',cd.nombre) AS ruta, ev.nombre AS estado, tk.precio AS precio,"
            " tk.pago AS folio"
            " FROM ticket tk JOIN viaje v ON v.numero = tk.viaje JOIN ruta r ON r.codigo = v.ruta"
            " JOIN terminal tor ON tor.numero = r.origen JOIN terminal tde ON tde.numero = r.destino"
            " JOIN ciudad co ON co.clave = tor.ciudad JOIN ciudad cd ON cd.clave = tde.ciudad"
            " JOIN edo_viaje ev ON ev.numero = v.estado WHERE tk.pasajero = %s"
            " ORDER BY v.fecHoraSalida DESC LIMIT 15", [filas[0]["pasajero_num"]])
    return out


def buscar_taquillero(args):
    nombre = _texto(args, "nombre", 60)
    where, params = "", []
    if nombre:
        cl, params = _like_tokens(
            "CONCAT(tq.taqNombre,' ',tq.taqPrimerApell,' ',COALESCE(tq.taqSegundoApell,''))", nombre)
        where = " WHERE " + cl
    filas = _q(
        "SELECT tq.registro AS id, CONCAT(tq.taqNombre,' ',tq.taqPrimerApell,"
        "COALESCE(CONCAT(' ',tq.taqSegundoApell),'')) AS nombre, te.nombre AS terminal,"
        " ci.nombre AS ciudad, CASE WHEN tq.supervisa = 1 THEN 'si' ELSE 'no' END AS es_supervisor"
        " FROM taquillero tq JOIN terminal te ON te.numero = tq.terminal JOIN ciudad ci ON ci.clave = te.ciudad"
        + where + " ORDER BY ci.nombre, tq.taqPrimerApell LIMIT 60", params)
    vis, extra = _recortar(filas)
    return {"total": len(filas), "taquilleros": vis, "filas_omitidas": extra}


def catalogo(args):
    tipo = _opcion(args, "tipo", ("terminales", "rutas", "autobuses", "conductores", "flota_por_marca"), None)
    if tipo == "terminales":
        filas = _q("SELECT t.nombre AS terminal, ci.nombre AS ciudad, t.dirCalle AS calle,"
                   " t.dirNumero AS numero, t.telefono AS telefono, COUNT(tq.registro) AS taquilleros"
                   " FROM terminal t JOIN ciudad ci ON ci.clave = t.ciudad"
                   " LEFT JOIN taquillero tq ON tq.terminal = t.numero"
                   " GROUP BY t.numero, t.nombre, ci.nombre, t.dirCalle, t.dirNumero, t.telefono"
                   " ORDER BY taquilleros DESC, ci.nombre")
    elif tipo == "rutas":
        filas = _q("SELECT r.codigo AS ruta, CONCAT(co.nombre,' a ',cd.nombre) AS trayecto,"
                   " r.duracion AS duracion, r.precio AS precio FROM ruta r"
                   " JOIN terminal tor ON r.origen = tor.numero JOIN terminal tde ON r.destino = tde.numero"
                   " JOIN ciudad co ON tor.ciudad = co.clave JOIN ciudad cd ON tde.ciudad = cd.clave"
                   " ORDER BY r.precio DESC")
    elif tipo == "autobuses":
        marca = _texto(args, "filtro", 30)
        where, params = "", []
        if marca:
            cl, params = _like_tokens("ma.nombre", marca)
            where = " WHERE " + cl
        filas = _q("SELECT a.numero AS autobus, a.placas AS placas, ma.nombre AS marca, mo.nombre AS modelo,"
                   " mo.ano AS anio, mo.numasientos AS asientos FROM autobus a"
                   " JOIN modelo mo ON a.modelo = mo.numero JOIN marca ma ON mo.marca = ma.numero"
                   + where + " ORDER BY a.numero", params)
    elif tipo == "conductores":
        filas = _q("SELECT c.registro AS registro, " + NOMBRE_CON + " AS nombre,"
                   " c.licVencimiento AS licencia_vence, c.fechaContrato AS contratado"
                   " FROM conductor c ORDER BY c.conNombre")
    else:
        filas = _q("SELECT ma.nombre AS marca, COUNT(a.numero) AS unidades FROM autobus a"
                   " JOIN modelo mo ON a.modelo = mo.numero JOIN marca ma ON mo.marca = ma.numero"
                   " GROUP BY ma.numero, ma.nombre ORDER BY unidades DESC")
    vis, extra = _recortar(filas)
    return {"tipo": tipo, "total": len(filas), "registros": vis, "filas_omitidas": extra}


def ocupacion(args):
    hoy = _hoy()
    num = _int(args, "viaje")
    where, params = [], []
    if num is not None:
        where.append("v.numero = %s")
        params.append(num)
    else:
        ini, fin = _rango(_fecha(args, "desde"), _fecha(args, "hasta"), hoy - timedelta(days=14), hoy)
        where += ["v.fecHoraSalida >= %s", "v.fecHoraSalida < %s"]
        params += [ini, fin]
    limite = _int(args, "limite", 15, 1, MAX_FILAS)
    filas = _q(
        "SELECT v.numero AS viaje, DATE(v.fecHoraSalida) AS fecha, CONCAT(co.nombre,' a ',cd.nombre) AS ruta,"
        " ev.nombre AS estado, mo.numasientos AS capacidad, COUNT(va.asiento) AS ocupados,"
        " ROUND(COUNT(va.asiento) * 100.0 / NULLIF(mo.numasientos, 0), 1) AS pct_ocupacion"
        + _VIAJE_FROM + " LEFT JOIN modelo mo ON a.modelo = mo.numero"
        " LEFT JOIN viaje_asiento va ON va.viaje = v.numero AND va.ocupado = 1"
        " WHERE " + " AND ".join(where) +
        " GROUP BY v.numero, v.fecHoraSalida, co.nombre, cd.nombre, ev.nombre, mo.numasientos"
        " ORDER BY pct_ocupacion DESC LIMIT %s", params + [limite])
    return {"viajes": filas}


def autobuses_inactivos(args):
    modo = _opcion(args, "modo", ("sin_viaje_futuro", "sin_uso_reciente"), "sin_viaje_futuro")
    base = ("SELECT a.numero AS autobus, a.placas AS placas, ma.nombre AS marca, mo.nombre AS modelo,"
            " mo.numasientos AS asientos")
    union = (" FROM autobus a JOIN modelo mo ON a.modelo = mo.numero JOIN marca ma ON mo.marca = ma.numero")
    if modo == "sin_viaje_futuro":
        filas = _q(base + union + " WHERE NOT EXISTS (SELECT 1 FROM viaje v WHERE v.autobus = a.numero"
                   " AND v.fecHoraSalida >= %s AND v.estado NOT IN (3,4)) ORDER BY a.numero",
                   [_hoy().isoformat()])
        out = {"modo": modo}
    else:
        dias = _int(args, "dias", 14, 1, 365)
        corte = (_hoy() - timedelta(days=dias)).isoformat()
        filas = _q(base + ", MAX(v.fecHoraSalida) AS ultimo_viaje" + union +
                   " LEFT JOIN viaje v ON v.autobus = a.numero"
                   " GROUP BY a.numero, a.placas, ma.nombre, mo.nombre, mo.numasientos"
                   " HAVING ultimo_viaje IS NULL OR ultimo_viaje < %s ORDER BY ultimo_viaje", [corte])
        out = {"modo": modo, "sin_viajes_desde": corte}
    vis, extra = _recortar(filas)
    out.update({"total": len(filas), "autobuses": vis, "filas_omitidas": extra})
    return out


TOOLS = {
    "resumen_dia": resumen_dia, "buscar_viajes": buscar_viajes, "detalle_viaje": detalle_viaje,
    "pasajeros_viaje": pasajeros_viaje, "consultar_boleto": consultar_boleto, "ingresos": ingresos,
    "boletos_vendidos": boletos_vendidos, "ranking": ranking, "buscar_pasajero": buscar_pasajero,
    "buscar_taquillero": buscar_taquillero, "catalogo": catalogo, "ocupacion": ocupacion,
    "autobuses_inactivos": autobuses_inactivos,
}


# --------------------------------------------------------------------------- esquema para el spec

def _t(name, desc, props=None, required=None):
    return {"name": name, "description": desc, "risk": "read",
            "parameters": {"type": "object", "properties": props or {}, "required": required or []}}


_F = lambda d: {"type": "string", "description": d + " Formato AAAA-MM-DD."}
_N = {"type": "integer"}

SCHEMA = [
    _t("resumen_dia", "Resumen de un dia: viajes por estado, en ruta ahora, boletos, ingresos de caja.",
       {"fecha": _F("Dia (por defecto hoy).")}),
    _t("buscar_viajes", "Lista viajes por fechas/estado/origen/destino/conductor/autobus. Sin fechas = hoy. "
       "Para manana o la proxima semana usa desde/hasta.",
       {"desde": _F("Inicio."), "hasta": _F("Fin."),
        "estado": {"type": "string", "enum": list(ESTADOS), "description": "Estado del viaje."},
        "origen": {"type": "string", "description": "Ciudad de origen."},
        "destino": {"type": "string", "description": "Ciudad de destino."},
        "conductor": {"type": "string", "description": "Nombre o apellido del conductor."},
        "autobus": {"type": "string", "description": "Numero o placas."},
        "limite": {"type": "integer", "description": "Max 40."}}),
    _t("detalle_viaje", "Detalle de un viaje: ruta, estado, autobus, conductor, capacidad, pasajeros.",
       {"viaje": _N}, ["viaje"]),
    _t("pasajeros_viaje", "Pasajeros (nombre, asiento, tipo, precio) de un viaje.", {"viaje": _N}, ["viaje"]),
    _t("consultar_boleto", "Busca una compra por folio o un boleto por codigo de ticket.",
       {"folio": {"type": "integer", "description": "Folio de la compra."},
        "ticket": {"type": "integer", "description": "Codigo del boleto individual."}}),
    _t("ingresos", "Ingresos de caja en un periodo (por defecto el mes en curso), con desglose opcional.",
       {"desde": _F("Inicio."), "hasta": _F("Fin."),
        "agrupar_por": {"type": "string", "enum": ["ninguno", "dia", "mes", "anio", "metodo"]}}),
    _t("boletos_vendidos", "Boletos emitidos e importe en un periodo (por defecto hoy).",
       {"desde": _F("Inicio."), "hasta": _F("Fin."),
        "agrupar_por": {"type": "string", "enum": ["ninguno", "dia", "ruta"]}}),
    _t("ranking", "Top N de conductores, autobuses, pasajeros, rutas o taquilleros (por viajes/boletos).",
       {"entidad": {"type": "string", "enum": ["conductores", "autobuses", "pasajeros", "rutas", "taquilleros"]},
        "n": {"type": "integer", "description": "1 a 20."}, "desde": _F("Inicio."), "hasta": _F("Fin.")},
       ["entidad"]),
    _t("buscar_pasajero", "Busca pasajeros por nombre; con un solo resultado incluye sus ultimos viajes.",
       {"nombre": {"type": "string"}}, ["nombre"]),
    _t("buscar_taquillero", "Busca taquilleros por nombre (sin nombre = lista); muestra terminal y si supervisa.",
       {"nombre": {"type": "string"}}),
    _t("catalogo", "Catalogos: terminales, rutas, autobuses, conductores o flota por marca.",
       {"tipo": {"type": "string", "enum": ["terminales", "rutas", "autobuses", "conductores", "flota_por_marca"]},
        "filtro": {"type": "string", "description": "Marca (solo para autobuses)."}}, ["tipo"]),
    _t("ocupacion", "Ocupacion de asientos por viaje (un viaje, o los ultimos 14 dias por defecto).",
       {"viaje": _N, "desde": _F("Inicio."), "hasta": _F("Fin."), "limite": {"type": "integer"}}),
    _t("autobuses_inactivos", "Autobuses sin viaje futuro asignado, o sin uso en los ultimos N dias.",
       {"modo": {"type": "string", "enum": ["sin_viaje_futuro", "sin_uso_reciente"]},
        "dias": {"type": "integer", "description": "Para sin_uso_reciente (default 14)."}}),
]
