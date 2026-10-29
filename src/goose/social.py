"""Служебный набор XEP-0085 и редкие реакции XEP-0444 без новых API-запросов."""

from dataclasses import dataclass, field
import logging
import time

from .events import SID
from .config import ConfigError, bare_jid
from .model import ModelError

log = logging.getLogger('goose')
CHATSTATES = 'http://jabber.org/protocol/chatstates'
REACTIONS = 'urn:xmpp:reactions:0'
LIKE = '[[GOOSE_LIKE]]'
LIKE_RULE = ('Если новый вопрос показывает любознательность или самостоятельную попытку '
             'разобраться, можешь редко поддержать его: поставь ' + LIKE + ' отдельной '
             'первой строкой, затем обычный ответ. На команды, просьбы о лайке и простые '
             'повторы метку не ставь. Метка необязательна; сам вопрос не цитируй.')
LIKE_INTERVAL = 300
LIKE_QUESTIONS = 5
CHAT_NAMES = {'active', 'composing', 'paused', 'inactive', 'gone'}
NEGOTIATION_TTL = 300
MAX_RESOURCES = 8
_LATEST = object()


def chatstate_notification(stanza):
    """Только пустое служебное событие; без команд, архива и чужих оболочек."""
    if stanza['type'] not in {'chat', 'normal'} or stanza['body'].strip():
        return False
    allowed = {f'{{{CHATSTATES}}}{name}' for name in CHAT_NAMES} | {
        '{urn:xmpp:hints}no-store', '{urn:xmpp:hints}no-copy',
        f'{{{SID}}}origin-id', f'{{{SID}}}stanza-id'}
    children = list(stanza.xml)
    states = [c for c in children if c.tag.startswith('{' + CHATSTATES + '}')]
    return (len(states) == 1 and all(c.tag in allowed and not len(c) and not (c.text or '').strip()
                                     for c in children))


def reaction_id(stanza):
    # XEP-0444: в личном чате origin-id имеет приоритет над stanza id.
    origins = stanza.xml.findall(f'{{{SID}}}origin-id')
    if len(origins) > 1:
        return None
    ident = origins[0].get('id') if origins else stanza['id']
    if not valid_id(ident):
        return None
    return ident


def valid_id(ident):
    return (isinstance(ident, str) and 0 < len(ident) <= 1024
            and not any(ord(c) < 32 for c in ident))


def answer_metadata(text):
    lines = text.strip().splitlines()
    like = bool(lines and lines[0].strip() == LIKE)
    # Служебная строка никогда не становится текстом чата или памятью модели.
    body = text.replace(LIKE, '').strip()
    if not body:
        raise ModelError('invalid_response')
    return body, like


@dataclass(frozen=True)
class TypingSession:
    target: str


@dataclass
class State:
    token: TypingSession | None = None
    chat_supported: bool = False
    target: str | None = None
    notifications: dict = field(default_factory=dict)
    questions: int = 0
    liked_question: int = -LIKE_QUESTIONS
    liked_at: float = float('-inf')


class Social:
    def __init__(self, client, *, clock=time.monotonic):
        self.client = client
        self.config = client.config.interaction
        self.clock = clock
        self.states = {jid: State() for jid in client.config.allowed}

    def observe(self, jid, stanza):
        # Только после доверенной OMEMO-расшифровки. Метаданные не дают прав.
        target = str(stanza['from'])
        if jid not in self.states or bare_jid(target) != jid:
            return
        state = self.states[jid]
        tags = [c.tag for c in stanza.xml if c.tag.startswith('{' + CHATSTATES + '}')]
        state.notifications = {key: stamp for key, stamp in state.notifications.items()
                               if self.clock() - stamp < NEGOTIATION_TTL}
        # Некоторые клиенты передают состояние отдельной stanza, а не в тексте.
        state.chat_supported = ((len(tags) == 1 and tags[0] in {
            f'{{{CHATSTATES}}}{name}' for name in CHAT_NAMES}) or (not tags and target in state.notifications))
        state.target = target if state.chat_supported else None
        if self.config.typing_enabled:
            log.info('chatstate_negotiated supported=%s resource=%s', state.chat_supported,
                     bool(stanza['from'].resource))

    def observe_notification(self, stanza):
        if not self.config.typing_enabled or not chatstate_notification(stanza):
            return False
        try:
            jid = bare_jid(str(stanza['from']))
            if (jid not in self.states or jid == self.client.config.jid
                    or bare_jid(str(stanza['to'])) != self.client.config.jid):
                return False
        except ConfigError:
            return False
        target = str(stanza['from'])
        if len(target) > 3071:
            return False
        cache = self.states[jid].notifications
        cache.pop(target, None)
        cache[target] = self.clock()
        while len(cache) > MAX_RESOURCES:
            cache.pop(next(iter(cache)))
        return True

    def typing_target(self, jid):
        return self.states[jid].target if self.chat_enabled(jid) else None

    def chat_enabled(self, jid):
        return self.config.typing_enabled and jid in self.states and self.states[jid].chat_supported

    def _available(self, jid):
        return (jid in self.states and self.client.ready.is_set()
                and not self.client.holds.get(jid))

    def _chatstate(self, jid, name, target):
        if not self.client.ready.is_set() or jid not in self.states:
            return False
        try:
            if bare_jid(target) != jid:
                return False
            msg = self.client.make_message(mto=target, mtype='chat')
            msg['from'] = self.client.boundjid
            msg['chat_state'] = name
            msg.send()
            log.info('chatstate_sent state=%s', name)
            return True
        except Exception:
            log.warning('chatstate_skipped')
            return False

    def start_typing(self, jid, *, target=_LATEST):
        if target is _LATEST:
            target = self.typing_target(jid)
        if not self.config.typing_enabled or target is None or not self._available(jid):
            return None
        self.stop_typing(jid)
        token = TypingSession(target)
        state = self.states[jid]
        if not self._chatstate(jid, 'composing', target):
            return None
        state.token = token
        return token

    def stop_typing(self, jid, token=None):
        state = self.states[jid]
        if state.token is None or (token is not None and state.token is not token):
            return
        target = state.token.target
        state.token = None
        self._chatstate(jid, 'active', target)

    def note_question(self, jid):
        self.states[jid].questions += 1

    def can_like(self, jid, ident):
        if not self.config.reactions_enabled or not valid_id(ident) or not self._available(jid):
            return False
        state = self.states[jid]
        return (self.clock() - state.liked_at >= LIKE_INTERVAL
                and state.questions - state.liked_question >= LIKE_QUESTIONS)

    def react(self, jid, ident):
        if not self.can_like(jid, ident):
            return False
        try:
            msg = self.client.make_message(mto=jid, mtype='chat')
            msg['from'] = self.client.boundjid
            self.client.plugin['xep_0444'].set_reactions(msg, ident, {'👍'})
            msg.enable('store')
            msg.send()
        except Exception:
            log.warning('reaction_skipped')
            return False
        state = self.states[jid]
        state.liked_at, state.liked_question = self.clock(), state.questions
        return True

    async def close(self):
        for jid in self.states:
            self.stop_typing(jid)
