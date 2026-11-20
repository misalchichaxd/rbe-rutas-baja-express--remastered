import json
from pathlib import Path

from django.core.management.base import BaseCommand

from taquilla.elipse_spec import perfil_caps, perfil_spec


class Command(BaseCommand):
    help = "Genera rbe_perfil.json y rbe_caps.json para registrar el perfil 'rbe' en ELIPSE ENGINE."

    def add_arguments(self, parser):
        parser.add_argument("--dir", default=".", help="Carpeta de salida (por defecto la actual).")

    def handle(self, *args, **opts):
        d = Path(opts["dir"])
        d.mkdir(parents=True, exist_ok=True)
        for nombre, datos in (("rbe_perfil.json", perfil_spec()), ("rbe_caps.json", perfil_caps())):
            (d / nombre).write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8")
            self.stdout.write(self.style.SUCCESS("Escrito %s" % (d / nombre)))
