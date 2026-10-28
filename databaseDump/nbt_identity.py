"""Compare SNBT compounds without depending on field order; preserve scalar types and list order."""
import re

_TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[{}\[\],:;]|[^\s{}\[\],:;]+')


def nbt_identity(text):
    tokens = _TOKEN.findall(text)
    cursor = 0

    def take(expected=None):
        nonlocal cursor
        token = tokens[cursor]
        cursor += 1
        if expected is not None and token != expected:
            raise ValueError('Unexpected SNBT token')
        return token

    def value():
        token = take()
        if token == '{':
            fields = []
            while tokens[cursor] != '}':
                key = take()
                take(':')
                # Minecraft 1.7 writes namespaced compound keys without quotes.
                # The last colon before the value separates key from value.
                while cursor + 1 < len(tokens) and tokens[cursor + 1] == ':':
                    key += ':' + take()
                    take(':')
                fields.append((key, value()))
                if tokens[cursor] != ',':
                    break
                take(',')
            take('}')
            return ('compound', tuple(sorted(fields)))
        if token == '[':
            entries = []
            while tokens[cursor] != ']':
                # Preserve typed-array prefixes and legacy indexed-list labels.
                if cursor + 1 < len(tokens) and tokens[cursor + 1] in (':', ';'):
                    entries.append(('prefix', take(), take()))
                entries.append(value())
                if tokens[cursor] != ',':
                    break
                take(',')
            take(']')
            return ('list', tuple(entries))
        if token in ('}', ']', ',', ':', ';'):
            raise ValueError('Invalid SNBT value')
        return ('scalar', token)

    try:
        result = value()
        return result if cursor == len(tokens) else ('raw', text)
    except (ValueError, IndexError):
        return ('raw', text)
