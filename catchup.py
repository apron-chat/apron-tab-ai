"""Conservative startup follow-ups; payloads stay only in bounded memory."""
import hashlib
import re

WINDOW_MS = 15 * 60 * 1000
MAX_REPLIES = 3


def key(room, ident):
    return hashlib.sha256(('server.apron.chat\0' + room + '\0' + ident).encode()).hexdigest()


def number(value):
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal() or len(value) > 16:
        return None
    value = int(value)
    return value if 0 < value < 2**53 else None


def select(snapshots, room, you, now_ms, humans=frozenset(), secrets=()):
    if not isinstance(snapshots, list) or len(snapshots) > 128:
        return []
    latest = {}
    for row in snapshots:
        if (not isinstance(row, dict) or not isinstance(row.get('from', {}), dict)
                or not isinstance(row.get('body', {}), dict)):
            continue
        ident, version = row.get('message_id'), number(row.get('log_id'))
        if number(ident) is None or version is None:
            continue
        if ident not in latest or version > number(latest[ident]['log_id']):
            latest[ident] = row
    own = {i for i, r in latest.items() if r.get('room_id') == room and r.get('from', {}).get('user_id') == you}
    answered = {r.get('reply_to', {}).get('message_id') for r in latest.values()
                if r.get('room_id') == room and r.get('from', {}).get('user_id') == you
                and isinstance(r.get('reply_to', {}), dict)}
    newest = {}
    for ident, row in latest.items():
        user = row.get('from', {}).get('user_id')
        if isinstance(user, str) and row.get('room_id') == room:
            newest[user] = max(newest.get(user, 0), number(ident))
    result = []
    for ident, row in sorted(latest.items(), key=lambda item: int(item[0]), reverse=True):
        author, body = row.get('from', {}), row.get('body', {})
        if not isinstance(author, dict) or not isinstance(body, dict):
            continue
        user, roles, text = author.get('user_id'), author.get('roles', []), body.get('text')
        created = number(ident)
        if (row.get('room_id') != room or created != number(row.get('log_id'))
                or not now_ms - WINDOW_MS <= created <= now_ms or ident in answered
                or row.get('deleted') or row.get('prev_room_id') or body.get('embeds')
                or not isinstance(user, str) or user == you or user.startswith('~')
                or humans and user not in humans or not isinstance(roles, list)
                or any(str(r).lower() == 'bot' for r in roles) or newest.get(user) != created
                or not isinstance(text, str) or not text.strip() or len(text.encode()) > 8000
                or any(s and s in text for s in secrets)):
            continue
        mentions = body.get('mentions', [])
        ref = row.get('reply_to', {})
        addressed = isinstance(mentions, list) and you in mentions
        addressed |= isinstance(ref, dict) and ref.get('message_id') in own
        # Only an explicit unresolved question, never a generic mention, URL,
        # old action request, or a command quoted in history. Model stays text-only.
        if (not addressed or '?' not in text or re.search(r'https?://|/\w+|\b(thread|summary|summarize|fetch|move|delete|cancel|never\s*mind|resolved|answered)\b', text, re.I)):
            continue
        result.append(row)
        if len(result) == MAX_REPLIES:
            break
    return list(reversed(result))
