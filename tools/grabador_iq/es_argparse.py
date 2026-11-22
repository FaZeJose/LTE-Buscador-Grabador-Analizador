"""Traduce al español los textos fijos de argparse (ayuda y errores)."""
import argparse

_ES = {
    "usage: ": "uso: ",
    "options": "opciones",
    "positional arguments": "argumentos",
    "show this help message and exit": "muestra esta ayuda y sale",
    "%(prog)s: error: %(message)s\n": "%(prog)s: error: %(message)s\n",
    "the following arguments are required: %s": "faltan estos argumentos obligatorios: %s",
    "unrecognized arguments: %s": "argumentos no reconocidos: %s",
    "expected one argument": "se esperaba un valor",
    "invalid %(type)s value: %(value)r": "valor %(type)s no valido: %(value)r",
    "invalid choice: %(value)r (choose from %(choices)s)":
        "opcion no valida: %(value)r (elige entre %(choices)s)",
    "argument %(argument_name)s: %(message)s": "argumento %(argument_name)s: %(message)s",
    "ambiguous option: %(option)s could match %(matches)s":
        "opcion ambigua: %(option)s puede ser %(matches)s",
    " (default: %(default)s)": " (por defecto: %(default)s)",
}


def _(msg):
    return _ES.get(msg, msg)


argparse._ = _


class Formato(argparse.ArgumentDefaultsHelpFormatter):
    def _get_help_string(self, action):
        h = action.help or ""
        if "%(default)" not in h and action.default not in (None, False, argparse.SUPPRESS) \
                and action.option_strings:
            h += " (por defecto: %(default)s)"
        return h
