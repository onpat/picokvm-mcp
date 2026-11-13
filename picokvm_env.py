"""Load a small, literal subset of .env files without external dependencies."""

import os
from pathlib import Path
import re


class EnvError(ValueError):
    """A configuration error whose message never includes setting values."""


def _value(raw, path, line_number):
    value = raw.strip()
    if value.startswith(("'", '"')):
        end = value.find(value[0], 1)
        if end == -1:
            raise EnvError("{}:{}: unclosed quoted value".format(path, line_number))
        remainder = value[end + 1:]
        if remainder and not (remainder[0].isspace()
                              and remainder.lstrip().startswith("#")):
            raise EnvError("{}:{}: unexpected text after quoted value".format(path, line_number))
        value = value[1:end]
    else:
        comment = re.search(r"\s#", raw)
        value = raw[:comment.start()].strip() if comment else value
    if "\0" in value:
        raise EnvError("{}:{}: invalid value".format(path, line_number))
    return value


def load_env(path):
    """Fill unset environment variables from path; missing files are optional.

    Parse the entire file before changing the environment. Existing variables,
    including empty ones, take priority; the first definition in the file wins.
    """
    path = Path(path)
    try:
        content = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return
    except OSError as error:
        raise EnvError("{}: cannot read .env file ({})".format(path, error.strerror)) from None
    except UnicodeError:
        raise EnvError("{}: .env file must be UTF-8".format(path)) from None

    settings = {}
    for line_number, line in enumerate(content.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, raw = line.partition("=")
        key = key.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise EnvError("{}:{}: expected KEY=VALUE with a valid variable name".format(path, line_number))
        value = _value(raw, path, line_number)
        settings.setdefault(key, value)
    for key, value in settings.items():
        os.environ.setdefault(key, value)
