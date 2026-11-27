"""Punto de entrada de la CLI `sanitize`.

Por ahora es un esqueleto; la CLI completa llega en la fase 8.
"""

import argparse

from sanitizer import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sanitize",
        description="Anonimiza un P&ID/PFD en PDF según una configuración YAML.",
    )
    parser.add_argument("pdf", nargs="?", help="PDF de entrada")
    parser.add_argument("--config", help="Reglas YAML del cliente (configs/<cliente>.yaml)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.parse_args(argv)
    parser.exit(2, "sanitize: todavía no implementado (fase 8).\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
