"""Manejo de zona horaria por cliente con zoneinfo (IANA), sin offsets fijos.
La DB guarda SIEMPRE UTC; la UI captura/muestra SIEMPRE hora local del cliente.
La timezone es atributo del cliente (no del servidor ni del slot)."""
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

DEFAULT_TZ = 'America/Santiago'


def get_zone(tzname):
    """ZoneInfo de tzname, con fallback robusto a Santiago y luego a UTC."""
    if ZoneInfo is None:
        return timezone.utc
    if not tzname:
        tzname = DEFAULT_TZ
    try:
        return ZoneInfo(tzname)
    except Exception:
        try:
            return ZoneInfo(DEFAULT_TZ)
        except Exception:
            return timezone.utc


def valid_tz(tzname):
    if ZoneInfo is None or not tzname:
        return False
    try:
        ZoneInfo(tzname)
        return True
    except Exception:
        return False


def local_to_utc(local_str, tzname):
    """local_str = 'YYYY-MM-DDTHH:MM' (wall-clock LOCAL del cliente, sin zona).
    Devuelve ISO UTC con sufijo +00:00 explícito. zoneinfo resuelve DST por fecha."""
    naive = datetime.fromisoformat(local_str)
    aware_local = naive.replace(tzinfo=get_zone(tzname), fold=0)
    return aware_local.astimezone(timezone.utc).isoformat()


def utc_to_local(utc_str, tzname):
    """ISO UTC -> datetime aware en la tz del cliente. Tolera strings naive (back-compat)."""
    dt = datetime.fromisoformat(utc_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(get_zone(tzname))


def now_utc():
    return datetime.now(timezone.utc)


def offset_label(tzname):
    """Etiqueta tipo 'Santiago · GMT-4' para mostrar el huso en la UI."""
    try:
        z = get_zone(tzname)
        off = now_utc().astimezone(z).utcoffset()
        total = int(off.total_seconds() // 60)
        sign = '+' if total >= 0 else '-'
        h, m = divmod(abs(total), 60)
        city = (tzname or DEFAULT_TZ).split('/')[-1].replace('_', ' ')
        return f"{city} · GMT{sign}{h}" + (f":{m:02d}" if m else "")
    except Exception:
        return tzname or DEFAULT_TZ
